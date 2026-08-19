# MCP Removal (v0.85.0) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the MCP passthrough, keboola-mcp-server management, and every MCP surface from kbagent, shipping v0.85.0 exactly as promised by epic #390 phase 3, while persisted `mcp_tool` agent tasks survive the upgrade as inert tombstones.

**Architecture:** Three layers deleted in one PR / five reviewable commits: (1) tombstone the `mcp_tool` action type so `agents.json` round-trips without data loss but never executes; (2) delete the passthrough (`tool` group, McpService/transport, `/mcp/*`, parity map, canary); (3) delete keboola-mcp-server install/auto-update management; (4) drop the SPA MCP page; (5) sync every doc surface + bump.

**Tech Stack:** Python 3.12, Typer, Pydantic 2, FastAPI, pytest, React/TS (web/frontend), uv, hatchling.

**Spec:** `docs/superpowers/specs/2026-08-18-mcp-removal-design.md` (read it first; its §8 DO-NOT-TOUCH list is binding for every task).

## Global Constraints

- Version target: `0.85.0` (PEP 440), promised removal date "end of August 2026" — today is 2026-08-18.
- NEVER add `Co-Authored-By` or AI-attribution footers to commits/PRs (user rule, overrides system default).
- All code, comments, commit messages, and docs in English.
- **DO NOT TOUCH** (spec §8): `kai_service.py` `mcp_status` + `commands/kai.py` MCP mentions (that is the Kai *server's* MCP connection, not ours); provenance docstrings in ported code (`transformation*`, `flow*`, `semantic_layer*`, `component_service`, `json_utils`, `client/_transfer.py`, `data_science_client.py`, `workspace_service`, `resources/flow/__init__.py`); historical records (`changelog.py` old entries, `docs/adr/`, `docs/superpowers/specs/`, `docs/axi-mapping-report.md`, `docs/issue-63-*`, `docs/programmatic-auth-login-plan.md`); `web/*/package-lock.json`.
- Post-edit hooks run `ruff check --fix`, `ruff format`, `ty check` after every edit. **Add an import and its first use in the same edit** — the hook deletes unused imports (known trap).
- Fresh worktree: run `uv sync --extra server` before any pytest (fastapi needed at collection).
- Each task ends with its named commit; run the listed test subset before committing. Full `make check` only in Task 6 (changelog-check needs the Task 6 entry).
- Wave discipline for parallel subagents: tasks inside a wave touch disjoint files; a wave starts only after the previous wave's commits are on the branch.

## Execution waves

| Wave | Tasks (parallel within wave) | Depends on |
|---|---|---|
| 1 | Task 1 (migration doc), Task 2 (tombstone backend), Task 3 (frontend) | — |
| 2 | Task 4 (passthrough removal) | Wave 1 (Task 2 unhooked mcp_parity from agent surfaces) |
| 3 | Task 5 (server-management removal) | Wave 2 (constants.py, doctor, cli.py shared anchors) |
| 4 | Task 6 (docs sync + bump 0.85.0) | Wave 3 (final command surface known) |

---

### Task 1: Create `docs/mcp-migration.md` from the parity map

**Files:**
- Create: `docs/mcp-migration.md`
- Read-only source: `src/keboola_agent_cli/mcp_parity.py` (still exists in wave 1 — that is WHY this task runs first)

**Interfaces:**
- Produces: `docs/mcp-migration.md` — referenced by Task 2's `REMOVED_ACTION_MESSAGE`, Task 6's changelog entry and `gotchas.md`.

- [ ] **Step 1: Extract the full parity table**

Run: `python3 - << 'EOF'` style script (or read the file) over `src/keboola_agent_cli/mcp_parity.py` and render every entry of `MCP_TOOL_PARITY` as a markdown row: `| tool_name | kbagent <command> | note |`. 37 entries expected — verify count matches `len(MCP_TOOL_PARITY)`.

- [ ] **Step 2: Write the document**

Structure (all content in English):

```markdown
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
... (all 37 rows) ...

## Still using keboola-mcp-server elsewhere (Claude Desktop, Cursor)?
kbagent no longer updates it for you. Keep it fresh yourself:

    uv tool install --upgrade --prerelease=allow keboola-mcp-server

(`--prerelease=allow` is required: the server depends on a pre-release-only
transitive package; without the flag uv silently resolves an ancient version.)

## Migrating a scheduled task
1. `kbagent agent show TASK_ID` — read the old `params` (tool, project, input).
2. Find the native command in the table above.
3. `kbagent agent update TASK_ID` cannot change the action type — create a new
   task with `--type cli_command --argv <cmd> --argv <sub> --argv --project=ALIAS ...`
   and `kbagent agent delete OLD_ID --yes`.
```

- [ ] **Step 3: Verify all 37 tools present**

Run: `python3 -c "import re,pathlib; doc=pathlib.Path('docs/mcp-migration.md').read_text(); import importlib.util,sys; spec=importlib.util.spec_from_file_location('mp','src/keboola_agent_cli/mcp_parity.py'); m=importlib.util.module_from_spec(spec); sys.modules['mp']=m; spec.loader.exec_module(m); missing=[t for t in m.MCP_TOOL_PARITY if t not in doc]; print('MISSING:', missing); sys.exit(1 if missing else 0)"`
Expected: `MISSING: []`

- [ ] **Step 4: Commit**

```bash
git add docs/mcp-migration.md
git commit -m "docs: add MCP passthrough migration guide (epic #390 phase 3)"
```

---

### Task 2: Tombstone the `mcp_tool` action type

**Files:**
- Modify: `src/keboola_agent_cli/server/agents_store.py` (constants + annotate helper; `ActionType` literal UNCHANGED)
- Modify: `src/keboola_agent_cli/server/agent_runner.py` (`run_task_once` dispatch ~line 1126; `scheduler_loop` ~line 1258; module docstring line 5)
- Modify: `src/keboola_agent_cli/server/run_broadcaster.py` (~lines 113–124)
- Modify: `src/keboola_agent_cli/commands/agent.py` (drop `--tool/--mcp-project/--mcp-branch/--input` flags; reject `mcp_tool`; swap deprecation import)
- Modify: `src/keboola_agent_cli/server/routers/agents.py` (line 15 import; line 117 annotate; POST guard)
- Modify: `src/keboola_agent_cli/services/agent_service.py` (drop `mcp_service` param + `CliAgentRegistry.mcp`)
- Modify: `src/keboola_agent_cli/cli.py:359` (`AgentService(config_store=config_store)`)
- Modify: `src/keboola_agent_cli/services/doctor_service.py` (`_check_mcp_tool_tasks` warn→fail; drop mcp_parity import)
- Delete: `tests/test_mcp_tool_task_detection.py`
- Modify: `tests/test_mcp_deprecation_warnings.py` (delete ONLY the agent-flavour warning tests; keep `tool list`/`tool call` tests — Task 4 deletes those)
- Modify: `tests/test_agent_service.py` (constructor no longer takes `mcp_service`)
- Test: create `tests/test_agent_tombstone.py`

**Interfaces:**
- Consumes: `docs/mcp-migration.md` (referenced in message text; Task 1 creates it in the same wave — reference is textual, no import).
- Produces (Task 4/5/6 rely on these exact names in `agents_store.py`):
  - `REMOVED_ACTION_TYPES: frozenset[str]`
  - `REMOVED_ACTION_MESSAGE: str`
  - `REMOVED_IN_VERSION: str = "0.85.0"`
  - `def annotate_removed_action(task: dict) -> dict`
  - `agent_runner.is_dispatchable(task: AgentTask) -> bool`
  - `AgentService.__init__(self, config_store: ConfigStore)` (mcp_service param GONE)
  - `CliAgentRegistry` without `mcp` field

- [ ] **Step 1: Write failing tests** (`tests/test_agent_tombstone.py`)

```python
"""Tombstone semantics for the removed mcp_tool agent action (v0.85.0)."""

import asyncio
from types import SimpleNamespace

from keboola_agent_cli.server.agent_runner import run_task_once
from keboola_agent_cli.server.agents_store import (
    REMOVED_ACTION_MESSAGE,
    REMOVED_ACTION_TYPES,
    AgentAction,
    AgentStore,
    AgentTask,
    annotate_removed_action,
)


def _mcp_task(name: str = "legacy") -> AgentTask:
    return AgentTask(
        name=name,
        enabled=True,
        action=AgentAction(type="mcp_tool", params={"tool": "get_jobs", "project": "padak"}),
    )


class TestRoundTrip:
    def test_mcp_tool_task_survives_unrelated_write(self, tmp_path) -> None:
        store = AgentStore(config_dir=tmp_path)
        store.save_tasks([_mcp_task()])
        # unrelated write: upsert a different task, then reload
        store.upsert_task(
            AgentTask(name="new", action=AgentAction(type="cli_command", params={"argv": ["version"]}))
        )
        names = {t.name for t in store.load_tasks()}
        assert "legacy" in names, "tombstone task must NOT be dropped by an unrelated save"

    def test_action_type_still_validates(self) -> None:
        assert _mcp_task().action.type == "mcp_tool"
        assert "mcp_tool" in REMOVED_ACTION_TYPES


class TestExecutionRefusal:
    def test_run_task_once_persists_error(self, tmp_path) -> None:
        store = AgentStore(config_dir=tmp_path)
        task = store.upsert_task(_mcp_task())
        run = asyncio.run(run_task_once(task, SimpleNamespace(), store))
        assert run.status == "error"
        assert run.error is not None and "REMOVED" in run.error
        # persisted, not just returned
        assert store.load_runs(task.id)[0].status == "error"


class TestSchedulerSkip:
    def test_tombstone_task_is_not_dispatchable_even_when_enabled(self) -> None:
        from keboola_agent_cli.server.agent_runner import is_dispatchable

        task = _mcp_task()
        assert task.enabled is True
        assert is_dispatchable(task) is False

    def test_live_types_are_dispatchable(self) -> None:
        from keboola_agent_cli.server.agent_runner import is_dispatchable

        t = AgentTask(name="x", action=AgentAction(type="cli_command", params={"argv": ["version"]}))
        assert is_dispatchable(t) is True


class TestAnnotate:
    def test_annotate_marks_removed(self) -> None:
        payload = _mcp_task().model_dump(mode="json")
        assert annotate_removed_action(payload)["deprecation"] == REMOVED_ACTION_MESSAGE

    def test_annotate_leaves_live_types_alone(self) -> None:
        t = AgentTask(name="x", action=AgentAction(type="cli_command", params={"argv": ["version"]}))
        assert "deprecation" not in annotate_removed_action(t.model_dump(mode="json"))
```

Note: check the actual name of the run-history reader on `AgentStore` (`load_runs` vs `list_runs`) with `grep -n "def .*runs" src/keboola_agent_cli/server/agents_store.py` and use the real one. Check `run_task_once`'s exact signature (`grep -n "async def run_task_once" src/keboola_agent_cli/server/agent_runner.py`) — pass keyword args if required.

- [ ] **Step 2: Run tests, verify they fail**

Run: `uv run pytest tests/test_agent_tombstone.py -v`
Expected: FAIL (ImportError: `REMOVED_ACTION_MESSAGE` / `annotate_removed_action` do not exist yet).

- [ ] **Step 3: Add tombstone constants to `agents_store.py`**

Right after the `ActionType` literal (keep `"mcp_tool"` IN the literal, add trailing comment `# "mcp_tool" is a tombstone -- see REMOVED_ACTION_TYPES`):

```python
#: Action types removed in 0.85.0. Kept in ``ActionType`` on purpose: load_tasks()
#: skips entries that fail validation and save_tasks() rewrites the file from the
#: loaded list, so dropping the literal would silently delete the user's task from
#: disk on the next unrelated write. Round-trip must survive; execution must not.
REMOVED_ACTION_TYPES: frozenset[str] = frozenset({"mcp_tool"})
REMOVED_IN_VERSION: str = "0.85.0"
REMOVED_ACTION_MESSAGE = (
    f"agent action type 'mcp_tool' was REMOVED in kbagent v{REMOVED_IN_VERSION} "
    "(epic #390). This task no longer runs. Recreate it with --type cli_command "
    "using the native kbagent command -- see docs/mcp-migration.md for the "
    "tool->command map."
)


def annotate_removed_action(task: dict) -> dict:
    """Add an additive ``deprecation`` key to a task using a removed action type.

    Additive and only on affected tasks, so every existing consumer sees a
    byte-identical payload. Mutates and returns the same dict.
    """
    if (task.get("action") or {}).get("type") in REMOVED_ACTION_TYPES:
        task["deprecation"] = REMOVED_ACTION_MESSAGE
    return task
```

Also in the `apply_runtime_input` function (~line 320): delete the `elif task.action.type == "mcp_tool":` branch and the `mcp_tool` bullet in its docstring.

- [ ] **Step 4: Refuse execution in `agent_runner.py`**

In `run_task_once`, INSIDE the existing `try:` block, replace the whole `if task.action.type == "mcp_tool": ... output = await _run_mcp_tool(...)` branch (~lines 1126–1133) with:

```python
        if task.action.type in REMOVED_ACTION_TYPES:
            raise ValueError(REMOVED_ACTION_MESSAGE)
```

(import `REMOVED_ACTION_TYPES, REMOVED_ACTION_MESSAGE` from `.agents_store` in the same edit). Delete `async def _run_mcp_tool` (~line 695) entirely. In `scheduler_loop` (~line 1258), add BEFORE the `if not task.enabled:` check:

Add a module-level predicate (next to `scheduler_loop`) so the skip is unit-testable:

```python
def is_dispatchable(task: AgentTask) -> bool:
    """Whether the cron scheduler may dispatch this task at all.

    Removed action types (v0.85.0 tombstones) never dispatch, regardless of
    the persisted ``enabled`` flag.
    """
    return task.action.type not in REMOVED_ACTION_TYPES
```

and in `scheduler_loop`, BEFORE the `if not task.enabled:` check:

```python
                if not is_dispatchable(task):
                    continue
```

Update the module docstring line 5 (drop the `mcp_tool` bullet) and the comment at ~line 83 if it references MCP transport env behavior for mcp_tool.

- [ ] **Step 5: Refuse execution in `run_broadcaster.py`**

Replace the `_run_cli, _run_mcp_tool` import + if/else (~lines 113–124) with:

```python
                from .agent_runner import _run_cli
                from .agents_store import REMOVED_ACTION_MESSAGE, REMOVED_ACTION_TYPES

                if self.task.action.type in REMOVED_ACTION_TYPES:
                    raise ValueError(REMOVED_ACTION_MESSAGE)
                output = await _run_cli(self.registry, self.task.action.params)
```

Verify the surrounding `except` persists the error into `agent_run` (read the block below line 135); adjust to match its existing error path.

- [ ] **Step 6: CLI surface in `commands/agent.py`**

1. Replace the import of `MCP_TOOL_ACTION_DEPRECATION` / `annotate_mcp_tool_deprecation` from `..mcp_parity` with `REMOVED_ACTION_MESSAGE, REMOVED_ACTION_TYPES, annotate_removed_action` from `..server.agents_store` (check the current import path style — line 26–30).
2. In `_action_from_flags`: delete the `tool` / `mcp_project` / `mcp_branch` / `input_payload` parameters, the whole `if action_type == "mcp_tool":` branch, and immediately after BOTH the `--from-file` `model_validate` return and before it, guard:

```python
        action = AgentAction.model_validate(payload)  # existing line
        if action.type in REMOVED_ACTION_TYPES:
            formatter.error(message=REMOVED_ACTION_MESSAGE, error_code=ErrorCode.INVALID_ARGUMENT)
            raise typer.Exit(code=2) from None
        return action
```

and change the two error strings mentioning the type list to `"ai_agent|cli_command"` (the `--type is required` message and the final `Unknown --type` message).
3. In `agent create` / `agent test` command signatures: delete the `--tool`, `--mcp-project`, `--mcp-branch`, `--input` typer options and their pass-through arguments; delete the `if action.type == "mcp_tool": formatter.warning(...)` + json `payload["deprecation"]` blocks (creation of mcp_tool is now impossible).
4. In `agent update` (~line 698): keep the persisted-flavour warning but switch it to the new symbols:

```python
    if task.action.type in REMOVED_ACTION_TYPES:
        formatter.warning(REMOVED_ACTION_MESSAGE)
        if formatter.json_mode:
            payload["deprecation"] = REMOVED_ACTION_MESSAGE
```

5. In `agent list` (~line 274 and 525): replace `annotate_mcp_tool_deprecation` with `annotate_removed_action`; reword the human list warning to `f"{len(deprecated)} task(s) use the REMOVED 'mcp_tool' action and no longer run -- see docs/mcp-migration.md"` (compute `deprecated` via `REMOVED_ACTION_TYPES` membership).
6. `--help` text for `--type` (lines 564, 892): `"ai_agent|cli_command"`.

- [ ] **Step 7: REST surface in `server/routers/agents.py`**

Line 15: import `annotate_removed_action` (and `REMOVED_ACTION_MESSAGE, REMOVED_ACTION_TYPES`) from `..agents_store` instead of `...mcp_parity`. Line 117: `annotate_removed_action`. In the `create_task` POST handler (and any other route that accepts an `AgentAction` — check `grep -n "AgentAction\|action" src/keboola_agent_cli/server/routers/agents.py`), add after body validation:

```python
    if body.action.type in REMOVED_ACTION_TYPES:
        raise HTTPException(status_code=422, detail=REMOVED_ACTION_MESSAGE)
```

(match the router's existing HTTPException import/usage style; also guard the serve-side ad-hoc test route if one exists).

- [ ] **Step 8: Drop McpService from `agent_service.py` + `cli.py`**

In `services/agent_service.py`: remove `from .mcp_service import McpService`; remove the `mcp: McpService` field from `CliAgentRegistry` (and its docstring bullet); change `AgentService.__init__` to `def __init__(self, config_store: ConfigStore) -> None:`; `_build_registry` returns `CliAgentRegistry(config_store=self._config_store)`. Update the class/module docstrings mentioning mcp_tool dispatch.
In `cli.py:359`: `agent_service = AgentService(config_store=config_store)`. (Leave `mcp_service` variable and doctor wiring alone — Task 4 owns them.)
In `tests/test_agent_service.py`: fix every `AgentService(...)` construction accordingly (grep for `mcp_service`).

- [ ] **Step 9: Doctor check flips warn → fail**

In `services/doctor_service.py` `_check_mcp_tool_tasks` (~lines 182–285): remove `MCP_REMOVAL_VERSION, MCP_REMOVAL_TARGET_DATE` from the `..mcp_parity` import (leave the rest of that import for Task 4 if other names are used — check; if the import becomes empty, delete it). Import `REMOVED_IN_VERSION` from `..server.agents_store` (module-level is fine, or extend the existing local import of `AgentStore`). Changes:
- affected-tasks branch: `"status": "fail"`, message: `f"{len(affected)} scheduled task(s) use the REMOVED 'mcp_tool' action (removed in v{REMOVED_IN_VERSION}) and NO LONGER RUN: {shown}{more}. Recreate each with --type cli_command -- see docs/mcp-migration.md."`, details key `"removed_in": REMOVED_IN_VERSION` (replacing `"removal_version"`).
- unreadable/corrupt-file branches: keep `warn`, reword "deprecated ... before vX" → "removed ... whether any need migrating".
- clean branch message: `"No tasks use the removed 'mcp_tool' action (...)"`.
- Update the method docstring ("flag scheduled tasks that still use the REMOVED MCP passthrough").
- `kbagent doctor --fix` must NOT touch agents.json (it already doesn't for this check — verify, do nothing).

- [ ] **Step 10: Prune agent-flavour tests in `test_mcp_deprecation_warnings.py`; delete `tests/test_mcp_tool_task_detection.py`**

In `test_mcp_deprecation_warnings.py`, delete only test classes/functions covering `agent create/update/test/list` warnings (grep `agent` in the file); Task 4 deletes the file's remainder. Add nothing there — new coverage lives in `test_agent_tombstone.py`. Delete `tests/test_mcp_tool_task_detection.py` (doctor coverage replaced next step).

- [ ] **Step 11: Add doctor tombstone test** (append to `tests/test_agent_tombstone.py`)

```python
class TestDoctorTombstone:
    def test_doctor_fails_on_tombstone_task(self, tmp_path) -> None:
        from unittest.mock import MagicMock
        from keboola_agent_cli.services.doctor_service import DoctorService

        store_dir = tmp_path
        AgentStore(config_dir=store_dir).save_tasks([_mcp_task()])
        config_store = MagicMock()
        config_store.config_dir = store_dir
        svc = DoctorService(config_store=config_store)
        check = svc._check_mcp_tool_tasks()
        assert check["status"] == "fail"
        assert "docs/mcp-migration.md" in check["message"]
```

Check `DoctorService.__init__` signature and `config_dir` attribute name against the real code (`config_store.config_dir` is used at line ~197) and mirror existing patterns in `tests/test_doctor_service.py` for constructing the service (it may need `mcp_service=MagicMock()` until Task 4 — pass it if the parameter still exists).

- [ ] **Step 12: CLI exit-2 and REST 422 regression tests**

Append to `tests/test_agent_tombstone.py` (mirror the CliRunner invocation pattern from `tests/test_cli.py` — global flags/env may be needed; copy an existing `agent create` test's setup):

```python
class TestCreationRefusal:
    def test_agent_create_mcp_tool_exits_2(self, tmp_config_dir) -> None:
        from typer.testing import CliRunner
        from keboola_agent_cli.cli import app

        runner = CliRunner()
        result = runner.invoke(
            app,
            ["--config-dir", str(tmp_config_dir), "agent", "create",
             "--name", "x", "--type", "mcp_tool"],
        )
        assert result.exit_code == 2
        assert "REMOVED" in result.output

    def test_agent_create_from_file_mcp_tool_exits_2(self, tmp_path, tmp_config_dir) -> None:
        import json
        from typer.testing import CliRunner
        from keboola_agent_cli.cli import app

        payload = tmp_path / "action.json"
        payload.write_text(json.dumps({"type": "mcp_tool", "params": {"tool": "get_jobs"}}))
        runner = CliRunner()
        result = runner.invoke(
            app,
            ["--config-dir", str(tmp_config_dir), "agent", "create",
             "--name", "x", "--from-file", f"@{payload}"],
        )
        assert result.exit_code == 2
        assert "REMOVED" in result.output
```

(check the real `--from-file` syntax — `@path` vs bare path — in `commands/agent.py` `_read_payload`; check the `tmp_config_dir` fixture name in `tests/conftest.py` and reuse it.)

For the REST guard, add to `tests/test_server_smoke.py` (mirror its existing TestClient fixture):

```python
def test_post_agents_rejects_mcp_tool(client) -> None:
    resp = client.post("/agents", json={
        "name": "x",
        "action": {"type": "mcp_tool", "params": {"tool": "get_jobs"}},
    })
    assert resp.status_code == 422
    assert "REMOVED" in resp.text
```

(adapt the fixture name and auth headers to the file's existing pattern.)

Run: `uv run pytest tests/test_agent_tombstone.py tests/test_server_smoke.py -q`
Expected: PASS.

- [ ] **Step 13: Run the affected suite**

Run: `uv run pytest tests/test_agent_tombstone.py tests/test_agent_service.py tests/test_doctor_service.py tests/test_mcp_deprecation_warnings.py tests/test_cli.py tests/test_server_smoke.py -x -q`
Expected: PASS. Fix `test_doctor_service.py` message assertions (warn→fail wording) and any `test_cli.py` help-text assertions for the dropped flags.

- [ ] **Step 14: Commit**

```bash
git add -A src/keboola_agent_cli tests
git commit -m "refactor(agent): tombstone the mcp_tool action type (epic #390 phase 3)"
```

---

### Task 3: Drop the MCP Tools page from the SPA

**Files:**
- Delete: `web/frontend/src/pages/Mcp.tsx`
- Modify: `web/frontend/src/App.tsx` (lines 15, 62–63), `web/frontend/src/layout/Sidebar.tsx` (line 82), `web/frontend/src/state.tsx` (line 24 union member `"mcp"`), `web/frontend/src/pages/Dashboard.tsx` (lines 261–262 quick-action card), `web/frontend/src/types.ts` (`McpTool` interface, line 154), `web/frontend/src/pages/Agents.tsx`

**Interfaces:**
- Consumes: nothing from other tasks (REST `/mcp/*` still exists during wave 1; the page's removal is forward-compatible).
- Produces: an SPA that never calls `/mcp/*` and whose Agents form offers exactly two action flavours (`ai_agent`, `cli_command`).

- [ ] **Step 1: Delete the page + unwire routing**

Delete `Mcp.tsx`. In `App.tsx` remove the import and the `case "mcp":` branch. In `Sidebar.tsx` remove the `{ id: "mcp", label: "MCP Tools", icon: Sparkles }` entry (keep the `Sparkles` import only if still used elsewhere in the file — grep). In `state.tsx` remove `| "mcp"` from the page union. In `Dashboard.tsx` remove the quick-action card targeting `"mcp"`. In `types.ts` remove the `McpTool` interface (grep `McpTool` across `web/frontend/src` first; remove dead consumers).

- [ ] **Step 2: Agents.tsx — two flavours in the form, tombstone-aware rendering**

- `type ActionType = "mcp_tool" | "cli_command" | "ai_agent"` (line 28): KEEP `"mcp_tool"` in the *type* (persisted tasks still carry it) but remove the `mcp_tool` **creation** UI: the flavour toggle button (lines ~905–909), the "MCP tool name" form section (~978), and the `if (actionType === "mcp_tool")` submit branch (~562). Default `actionType` must not be `mcp_tool` (check its `useState` initial value).
- Keep read-path rendering (lines ~111, ~409) so persisted tombstone tasks display; where the task row renders, surface the backend's additive `deprecation` string when present (the `/agents` route already injects it) — e.g. a red badge/text `Removed — no longer runs` with the `deprecation` message as tooltip/subtitle, matching existing NERD-UI badge patterns in the file.
- Update copy: description strings (~177, ~694) mentioning "Three flavours / three actions: MCP tool calls..." → two flavours.
- "Raw output (cli/mcp action)" label (~1018) → "Raw output (cli action)".

- [ ] **Step 3: Build gate**

Run: `cd web/frontend && npm ci --no-audit --no-fund && npm run build`
Expected: clean TypeScript build, no unused-import errors. Also `grep -rn "\"/mcp" web/frontend/src` → no hits.

- [ ] **Step 4: Commit**

```bash
git add -A web/frontend
git commit -m "feat(ui): drop the MCP Tools page and the mcp_tool agent flavour"
```

---

### Task 4: Remove the MCP passthrough (backend)

**Files:**
- Delete: `src/keboola_agent_cli/services/mcp_service.py`, `src/keboola_agent_cli/services/mcp_transport.py`, `src/keboola_agent_cli/commands/tool.py`, `src/keboola_agent_cli/mcp_parity.py`, `src/keboola_agent_cli/server/routers/mcp.py`, `scripts/check_mcp_parity.py`, `scripts/benchmark.py`, `.github/workflows/mcp-parity-canary.yml`
- Delete: `tests/test_mcp_service.py`, `tests/test_mcp_transport.py`, `tests/test_mcp_parity_map.py`, `tests/test_mcp_deprecation_warnings.py`
- Modify: `src/keboola_agent_cli/cli.py`, `src/keboola_agent_cli/server/dependencies.py`, `src/keboola_agent_cli/server/app.py`, `src/keboola_agent_cli/permissions.py`, `src/keboola_agent_cli/constants.py`, `src/keboola_agent_cli/output.py`, `src/keboola_agent_cli/errors.py`, `src/keboola_agent_cli/services/doctor_service.py`, `src/keboola_agent_cli/services/_auth_registration.py`, `scripts/check_sentinel_guards.py`, `Makefile`, `pyproject.toml`, `tests/conftest.py`
- Modify tests: `tests/test_cli.py`, `tests/test_permissions.py`, `tests/test_permissions_cli.py`, `tests/test_server_smoke.py`, `tests/test_doctor_service.py`, `tests/test_base_service.py`, `tests/test_helpers.py`, `tests/test_output.py`, `tests/test_e2e.py`, `tests/test_auth_sentinel.py`, `tests/test_auth_sentinel_guards.py`, `tests/helpers.py` (grep-driven)

**Interfaces:**
- Consumes: Task 2's guarantee that no agent surface imports `mcp_parity` or `McpService` (`agent_service`, `agent_runner`, `run_broadcaster`, `commands/agent.py`, `routers/agents.py` are already clean).
- Produces: no module imports `mcp`; `DoctorService.__init__(self, config_store, client_factory=None)` (the `mcp_service` param GONE — verify current signature first and preserve `client_factory` if present); `pyproject.toml` without the `mcp` dependency; permission engine without `tool.*` / `tool:*`.

- [ ] **Step 1: Update tests first (expectations of absence)**

1. Delete the 4 test files listed above.
2. `tests/conftest.py`: delete the autouse `_force_stdio_transport` fixture (lines ~25–28).
3. `tests/test_base_service.py`: remove `from keboola_agent_cli.services.mcp_service import MCP_ERROR_CODE` (line 31); rewrite the three tests using `MCP_ERROR` / `MCP_ERROR_CODE` (~441–490) to exercise `project_error_entry` with `fallback_code=ErrorCode.API_ERROR` instead (behavior under test is the fallback mechanism, not the specific code). Keep the `SessionAuthUnsupportedError("The MCP server subprocess")` tests ONLY if they test the generic mechanism — retarget the feature string to a still-guarded feature, e.g. `"The Keboola AI Service"`.
4. `tests/test_helpers.py:140`: `map_error_code_to_type("MCP_ERROR")` — find the mapping (`grep -rn "map_error_code_to_type" src/`), remove the MCP_ERROR entry there and this assertion.
5. `tests/test_permissions*.py`: remove tests for `classify_mcp_tool`, `tool:read|write|destructive` patterns, `tool.list`/`tool.call` registry entries. ADD the stale-policy regression test:

```python
def test_persisted_tool_patterns_are_inert() -> None:
    """A pre-0.85 policy carrying tool:* patterns loads and never matches (spec §5)."""
    policy = PermissionPolicy(mode="allow", deny=["tool:write", "tool:create_*"])
    engine = PermissionEngine(policy)
    # no operation starts with "tool:" anymore -> patterns never match, nothing crashes
    assert engine.is_allowed("config.update")
    assert engine.is_allowed("job.run")
```

(mirror the real constructor names from existing tests in `tests/test_permissions.py`).
6. `tests/test_server_smoke.py`: drop `/mcp/*` route assertions; assert `/mcp/tools` now 404s if the file asserts route inventories.
7. `tests/test_e2e.py`, `tests/helpers.py`, `tests/test_cli.py`, `tests/test_doctor_service.py`, `tests/test_output.py`: grep `-i mcp` and prune tool-group/McpService/doctor-check-5/renderer references.
8. `tests/test_auth_sentinel*.py`: drop assertions expecting `"kbagent tool (MCP server subprocess)"` in `session_unsupported_features`.

- [ ] **Step 2: Run tests, verify the expected failures**

Run: `uv run pytest tests/test_base_service.py tests/test_permissions.py -x -q`
Expected: FAIL where production code still exposes removed symbols (e.g. registry still contains `tool.call`). That is the red state driving Step 3.

- [ ] **Step 3: Delete the passthrough production code**

Delete the 8 production/CI files listed under **Files: Delete**. Then unwire, file by file:
- `cli.py`: remove `from .commands.tool import tool_app` (line 42), `app.add_typer(tool_app, ...)` (line 148), `from .services.mcp_service import McpService` (line 70), `mcp_service = McpService(...)` (line 336), `ctx.obj["mcp_service"]` (line 396); `doctor_service = DoctorService(config_store=config_store)` (line 356).
- `server/dependencies.py`: remove `McpService` import (line 34), `mcp` dataclass field (line 118), `self.mcp = McpService(...)` (line 161); `self.doctor = DoctorService(config_store=cs)` (line 170).
- `server/app.py`: remove `mcp` from the routers import (line 51), the `{"name": "mcp", ...}` OpenAPI tag dict (~lines 299–306), the `app.include_router(mcp.router)` line (700), and the `mcp` mention in the tag-groups comment (line 385: `"AI & Tools" -- mcp, kai, ...` → drop `mcp`).
- `services/doctor_service.py`: remove the `McpService, ensure_mcp_installed` import (line 31), the `mcp_service` constructor param + `self._mcp_service` (lines 58–62), Check 5 (`check_server_available`, lines ~92–94) and renumber/reword the surrounding check-count docstring (line 8); remove any remaining `..mcp_parity` import; remove the `--fix` MCP-install path — inspect `grep -n "fix\|ensure_mcp" src/keboola_agent_cli/services/doctor_service.py src/keboola_agent_cli/commands/doctor.py`: if `--fix` drives ONLY the MCP install, remove the `--fix` option from `commands/doctor.py` and its service plumbing entirely (and note it for Task 6 docs); if it fixes other things too, keep the flag and delete only the MCP branch.
- `output.py`: delete the MCP renderers (~lines 539–630: the tools-table renderer and tool-call panels; grep `def format_tools\|MCP` to find exact boundaries) and their exports.
- `errors.py`: delete `ErrorCode.MCP_ERROR` (lines 125–128) and the mention in the comment at ~line 299.
- `permissions.py`: delete `"tool.list"` / `"tool.call"` registry entries (150–151), the `_MCP_*_PREFIXES` tuples + `_MCP_READ_EXACT` + `classify_mcp_tool` (~383–430), the `tool:read|write|destructive` branch in `_matches_pattern` (~457–466) and its docstring bullets (glob example `'tool:create_*'` too), the `mcp_categories` virtual entries block in the permissions listing (~530+), and the comment at line 383 pointing at mcp_service dispatch. The `cli:*` branch keeps its `operation.startswith("tool:")` early-return REMOVED as well (no operation can be `tool:` — simplify), but keep plain `fnmatch` fallthrough so stale glob patterns stay inert.
- `constants.py`: delete ONLY the passthrough constants: `DEFAULT_MCP_TOOL_TIMEOUT`, `DEFAULT_MCP_INIT_TIMEOUT`, `DEFAULT_MCP_MAX_SESSIONS`, `ENV_MCP_TRANSPORT`, `DEFAULT_MCP_TRANSPORT`, `MCP_SERVER_STARTUP_TIMEOUT`, `MCP_SERVER_HEALTH_TIMEOUT`, `ENV_MCP_TOOL_TIMEOUT`, `ENV_MCP_INIT_TIMEOUT`, `ENV_MCP_MAX_SESSIONS` (+ their comment blocks). LEAVE `MCP_PYPI_URL`, `MCP_PROBE_TIMEOUT`, `MCP_UPGRADE_TIMEOUT`, `MCP_UV_PRERELEASE_FLAG`, `MCP_PIP_PRERELEASE_FLAG` — Task 5 owns those (version_service still imports them in this wave).
- `services/_auth_registration.py`: delete the `"kbagent tool (MCP server subprocess)"` entry from `SESSION_UNSUPPORTED_FEATURES` (line 38).
- `scripts/check_sentinel_guards.py`: delete BOTH `"The MCP server subprocess"` and `"The MCP HTTP transport"` alias rows (lines 142–143). Then run `python scripts/check_sentinel_guards.py --list` — it must pass; if it reports an orphaned guard, the corresponding `require_static_token` call sites died with mcp_service/mcp_transport (expected).
- `Makefile`: delete the `install-mcp` and `parity-check` targets and their `.PHONY` tokens (line 3) and the parity-check comment.
- `pyproject.toml`: delete `"mcp>=1.0.0,<2.0.0",` from `dependencies`. Run `uv sync --extra server` and `uv lock` if a lockfile exists (`ls uv.lock`).

- [ ] **Step 4: Sweep for dangling references**

Run: `grep -rn "McpService\|mcp_service\|mcp_transport\|mcp_parity\|classify_mcp_tool\|MCP_ERROR\|tool_app\|KBAGENT_MCP" src/ scripts/ tests/ Makefile .github/ --include="*"`
Expected: ZERO hits in production code; the only permitted survivors are (a) Task-5-owned files (`version_service.py`, `auto_update.py`, `commands/version.py`, `install.sh`, `frozen_dist.py`, `update_runner.py`, `constants.py` update-block, `server/routers/health.py`), (b) DO-NOT-TOUCH provenance docstrings, (c) `changelog.py` history, (d) `agents_store.py`/`agent_runner.py` tombstone strings mentioning "mcp_tool" as data. Investigate anything else.

- [ ] **Step 5: Full unit suite + guards**

Run: `uv run pytest tests/ -q -x --ignore=tests/test_e2e.py -k "not e2e"` then `make check-sentinel-guards check-error-codes lint`
Expected: PASS. (`make check` waits for Task 6 — changelog.)

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat!: remove the MCP passthrough (tool group, McpService, /mcp routes, parity map) (epic #390 phase 3)"
```

---

### Task 5: Stop managing keboola-mcp-server

**Files:**
- Modify: `src/keboola_agent_cli/services/version_service.py`, `src/keboola_agent_cli/auto_update.py`, `src/keboola_agent_cli/commands/version.py`, `src/keboola_agent_cli/constants.py`, `src/keboola_agent_cli/frozen_dist.py`, `src/keboola_agent_cli/update_runner.py`, `src/keboola_agent_cli/server/routers/health.py`, `install.sh`
- Modify tests: `tests/test_version_service.py`, `tests/test_auto_update.py`, `tests/test_update_runner.py`

**Interfaces:**
- Consumes: Task 4's state (constants.py still carries the 5 MCP-update constants; version_service is the last importer).
- Produces: `VersionService.get_versions()` returns NO `keboola-mcp-server` entry (additive removal — key `dependencies`/`mcp` gone; check the exact payload key at line ~884 and remove it); `UpdatePlan` dataclass without the `mcp` field; `_write_cache` without `mcp_latest_version`/`mcp_install_method` params; `_read_cache` UNCHANGED (already tolerant).

- [ ] **Step 1: Write the legacy-cache regression test** (append to `tests/test_auto_update.py`)

```python
def test_read_cache_tolerates_legacy_mcp_keys(tmp_path, monkeypatch) -> None:
    """A cache written by <=0.84.x carries MCP keys; 0.85.0 must load it (spec §7)."""
    import json
    from keboola_agent_cli import auto_update

    cache = tmp_path / "version_cache.json"
    cache.write_text(json.dumps({
        "last_check": "2026-08-17T00:00:00+00:00",
        "latest_version": "0.84.2",
        "mcp_latest_version": "1.61.0",
        "mcp_install_method": "uv_tool",
    }))
    monkeypatch.setattr(auto_update, "_get_cache_path", lambda: cache)
    data = auto_update._read_cache()
    assert data is not None and data["latest_version"] == "0.84.2"
```

(match the real `last_check` format used by `_write_cache` — read it first; the point is: extra keys must not be rejected.)

- [ ] **Step 2: Run it** — `uv run pytest tests/test_auto_update.py::test_read_cache_tolerates_legacy_mcp_keys -v` — expected: PASS already (reader is tolerant); it locks the contract.

- [ ] **Step 3: Excise MCP from `version_service.py`**

Delete: `MCP_PACKAGE_NAME`, `MCP_BINARY_NAME` (48–49), `McpUpdatePlan` (73–85) + the `mcp` field of `UpdatePlan` (88), `_get_local_mcp_version` (292+), `_uv_tool_list_get_mcp_version` (383+), `_uv_tool_list_has_mcp` (426+), `_detect_mcp_install_method` (463+), `_perform_mcp_update` (518+), `build_mcp_upgrade_command` (570+), `_fetch_mcp_latest_version` (687+), `prepare_mcp_update_plan` (777+), the `mcp=` line in the plan builder (~800–803), and in `get_versions` (814+) the whole MCP block (841–844, 859–860, 869–899: `mcp_upgrade_cmd_by_method`, `mcp_entry`, its insertion into the payload, and the `_write_cache(mcp_latest_version=..., mcp_install_method=...)` kwargs). Remove the now-unused constants import names (28–32) and update the module docstring (3–5). Preserve the kbagent-side logic byte-for-byte.

- [ ] **Step 4: Excise MCP from `auto_update.py`**

Delete `_maybe_update_mcp` (376+) and its call site(s), the MCP names in the import block (29–48), the `mcp_latest_version`/`mcp_install_method` params of `_write_cache` (128–155, keep the function), and reword docstrings 105–113 (cache doc: extra legacy keys are ignored), 219–221, 255. The startup flow becomes: kbagent stage only.

- [ ] **Step 5: Remaining surfaces**

- `commands/version.py`: `update` docstring (151–172) — drop stage 1 (MCP) and the combined output example; verify the human output path prints only the kbagent transition.
- `constants.py`: delete `MCP_PYPI_URL`, `MCP_PROBE_TIMEOUT`, `MCP_UPGRADE_TIMEOUT`, `MCP_UV_PRERELEASE_FLAG`, `MCP_PIP_PRERELEASE_FLAG` + comment blocks (287–319) and the MCP mention in the install-log comment (~354).
- `frozen_dist.py`: comment 79 + docstring 128 — reword to no longer reference the MCP method map; verify no code path references removed names.
- `update_runner.py`: comment 226 ("shared with the MCP stage") — reword.
- `server/routers/health.py:37`: docstring → "Versions of kbagent and Python." (payload shrinks automatically via `get_versions`).
- `install.sh`: line 201 comment `(REST + MCP + UI)` → `(REST + UI)`; line 258 tagline drop `keboola-mcp-server bundled & auto-updating` (keep `no sudo required`).

- [ ] **Step 6: Fix tests**

`tests/test_version_service.py`, `tests/test_auto_update.py`, `tests/test_update_runner.py`: delete tests of the removed functions; update `get_versions` payload assertions; keep/adapt cache-write assertions (no MCP kwargs). Run: `uv run pytest tests/test_version_service.py tests/test_auto_update.py tests/test_update_runner.py tests/test_server_smoke.py -q`
Expected: PASS. Then sweep: `grep -rn -i "mcp" src/keboola_agent_cli/services/version_service.py src/keboola_agent_cli/auto_update.py src/keboola_agent_cli/constants.py` → zero hits (provenance comments excepted, none expected here).

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "feat!: stop installing and auto-updating keboola-mcp-server"
```

---

### Task 6: Documentation sync, changelog, version bump 0.85.0

**Files:**
- Modify: `pyproject.toml` (version), `src/keboola_agent_cli/changelog.py` (new entry), `plugins/kbagent/.claude-plugin/plugin.json` + `.claude-plugin/marketplace.json` (via `make version-sync`), `CLAUDE.md`, `README.md`, `src/keboola_agent_cli/commands/context.py`, `plugins/kbagent/skills/kbagent/SKILL.md`, `plugins/kbagent/agents/keboola-expert.md`, `plugins/kbagent/skills/kbagent/references/commands-reference.md`, `plugins/kbagent/skills/kbagent/references/gotchas.md`, `plugins/kbagent/.claude-plugin/CLAUDE.md`, `docs/guide.md`, `docs/TUTORIAL.md`, `docs/web-server.md`, `docs/build-your-own-client.md`, `docs/error-codes.md`, `docs/e2e-scenarios.md`, `docs/auth.md`, `web/README.md`
- Delete: `plugins/kbagent/skills/kbagent/references/mcp-workflow.md`
- Verify only: every other `references/*-workflow.md` (grep-driven line edits)

**Interfaces:**
- Consumes: the final command surface from Tasks 2/4/5 (no `tool` group, no `--type mcp_tool`, no `/mcp/*`, no MCP update stage, possibly no `doctor --fix`).
- Produces: v0.85.0, green `make check`.

- [ ] **Step 1: Version + changelog**

`pyproject.toml`: `version = "0.85.0"`. Add the `"0.85.0"` entry at the TOP of `CHANGELOG` in `src/keboola_agent_cli/changelog.py`, matching the existing entry format exactly (list of note strings). Content (adapt phrasing to house style):

```python
"0.85.0": [
    "BREAKING: the MCP passthrough is removed (epic #390 phase 3, announced since 0.74.0): "
    "`kbagent tool list` / `tool call` are gone, `agent --type mcp_tool` no longer exists, and the "
    "`/mcp/*` REST routes were dropped from `kbagent serve`. Every catalog tool has a native command "
    "-- the full tool->command map moved to docs/mcp-migration.md. Persisted `mcp_tool` tasks in "
    "agents.json are NOT deleted: they survive load/save round-trips as inert tombstones, the "
    "scheduler skips them, a manual run records an error naming the migration guide, `agent list` "
    "flags them, and `kbagent doctor` reports them as FAIL. Recreate each as `--type cli_command`.",
    "BREAKING: kbagent no longer installs or auto-updates `keboola-mcp-server`. `kbagent update` and "
    "the startup hook update kbagent only; `kbagent version` no longer reports the MCP server. If you "
    "use keboola-mcp-server with Claude Desktop / Cursor, keep it fresh yourself: "
    "`uv tool install --upgrade --prerelease=allow keboola-mcp-server` (the pre-release flag is "
    "required -- see docs/mcp-migration.md).",
    "Removed: the `mcp` Python dependency (smaller install and standalone binary); ErrorCode "
    "MCP_ERROR; the `tool:read|write|destructive` permission categories (stale persisted `tool:*` "
    "patterns load fine and simply never match); the weekly mcp-parity-canary workflow. Closes #478 "
    "by deletion (the fail-open MCP tool classifier no longer exists).",
],
```

Then: `make version-sync` and `make changelog-check version-check`.

- [ ] **Step 2: CLAUDE.md `## All CLI Commands`**

Remove: the `kbagent tool list|call` block + its deprecation comment; `--type mcp_tool` variants from `agent create/test` lines (`--tool/--mcp-project/--mcp-branch/--input`); the `doctor` mcp_tool_tasks comment (reword: doctor FAILs on removed mcp_tool tasks); the MCP mentions in the `update`/`version` comment block ("keboola-mcp-server stage", "MCP before the terminal reinstall"); update the `agent` group comment ("Three action flavours" → "Two action flavours (ai_agent / cli_command)"). Also update the MCP Integration section under Project Structure (delete it; point to docs/mcp-migration.md) and the `commands/` module list (drop `tool`).

- [ ] **Step 3: `commands/context.py` (AGENT_CONTEXT)**

Grep `-n -i "mcp\|tool " src/keboola_agent_cli/commands/context.py`; remove the tool-group section, mcp_tool flavour mentions, MCP feature bullets; add one line pointing agents at `docs/mcp-migration.md` for historical tool names. Run `uv run pytest tests/test_cli.py -q -k context` if context tests exist.

- [ ] **Step 4: Plugin surfaces**

- Delete `references/mcp-workflow.md`; remove links to it from `SKILL.md` and any workflow file (`grep -rn "mcp-workflow" plugins/`).
- `SKILL.md`: remove tool/MCP triggers + rows; then `make skill-gen skill-check` (regenerates the CI-checked decision table).
- `keboola-expert.md`: FIRST `wc -c plugins/kbagent/agents/keboola-expert.md` (62,000-byte hard cap; ~400 bytes free at 0.83.0 — removal FREES bytes, but verify after edit). Remove `tool list`/`tool call` from the tool-selection matrix, MCP version-gate examples, mcp_tool task mentions. Re-run `wc -c` — must stay < 62000.
- `commands-reference.md`: delete the `tool` group section; update `agent` flavours; update `doctor`/`version`/`update` notes (no MCP stage).
- `gotchas.md`: add `(since v0.85.0)` entry: tool group + mcp_tool removed, native map in docs/mcp-migration.md, keboola-mcp-server no longer auto-updated (manual command). REWRITE (do not delete) older entries that *instruct* using `tool call` so they name the native command; keep dated historical notes that merely record behavior.
- `plugins/kbagent/.claude-plugin/CLAUDE.md`: same sweep (3 hits).

- [ ] **Step 5: README + docs**

- `README.md`: rewrite the 8 MCP mentions — headline (line 5), auto-update para (55), SPA feature list (67), agent flavours (74, 187), slash-command rule text (104), Dev-branches row (184), MCP tools feature row (186: DELETE row), Kai row (194: keep — Kai's MCP context is server-side), encryption row (195: drop "and MCP"), auto-update row (197), footnote (242), doctor line (246: drop "MCP server").
- `docs/guide.md`, `docs/TUTORIAL.md`, `docs/web-server.md`, `docs/build-your-own-client.md`, `docs/e2e-scenarios.md`, `docs/auth.md`, `web/README.md`: grep `-n -i mcp` each; delete `/mcp/*` route docs, tool-group walkthroughs, MCP env vars (`KBAGENT_MCP_*`); replace with a one-line pointer to `docs/mcp-migration.md` where a section disappears entirely.
- `docs/error-codes.md`: delete the `### MCP` section + `MCP_ERROR` row.

- [ ] **Step 6: Full gate**

Run: `make check` (lint, format-check, changelog-check, tests, sentinel guards, loc-check, skill-check, version-check) and `cd web/frontend && npm run build`.
Expected: all green. Then the final global sweep:
`grep -rn -i "mcp" src/ tests/ scripts/ plugins/ docs/ README.md CLAUDE.md Makefile install.sh .github/ --include="*" | grep -v -E "changelog.py|docs/adr/|docs/superpowers/|axi-mapping|issue-63|programmatic-auth-login|mcp-migration|package-lock"` — review every hit against the spec §8 allowlist (provenance docstrings, Kai server status, tombstone strings, vendored-flow provenance). Anything else is a bug.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "docs: remove every MCP surface, add migration guide pointers, bump to 0.85.0"
```

---

## Post-plan notes for the orchestrator

- PR title: `feat!: remove MCP (passthrough + server management) for v0.85.0 (epic #390 phase 3)`. PR body in English, links epic #390 and #478, quotes the migration guide. No AI attribution footer.
- After merge: the release itself (tag + GitHub release) is a separate, human-triggered step — compare `gh release list` first (release-checklist memory), and expect the `winget` job to fail (known-red, not a release blocker).
- Issue hygiene after merge: close #478 ("closed by deletion in v0.85.0"), tick phase 3 on epic #390 (close the epic if nothing else remains).
