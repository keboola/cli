"""Shared SQL workspaces in sync (CLI-25): pull, diff, push, clone.

``keboola.sandboxes`` is always ignored unless the manifest sets
``syncWorkspaces``. With the key, only shared SQL workspaces (no
``parameters.id``, ``runtime.shared: true``) are synced, config only. A delete
removes the workspace's SQL editor sessions before the configuration.

Every API call goes to a ``MagicMock(spec=KeboolaClient)`` client (or an
``httpx_mock`` transport for the Editor client); no test reaches a real project.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, call, patch

import pytest
import yaml
from typer.testing import CliRunner

from helpers import setup_single_project
from keboola_agent_cli.cli import app
from keboola_agent_cli.client import KeboolaClient
from keboola_agent_cli.config_store import CURRENT_CONFIG_VERSION
from keboola_agent_cli.constants import CONFIG_FILENAME, EXIT_PERMISSION_DENIED, KEBOOLA_DIR_NAME
from keboola_agent_cli.errors import ErrorCode, KeboolaApiError
from keboola_agent_cli.models import TokenVerifyResponse
from keboola_agent_cli.permissions import (
    FLAG_ESCALATIONS,
    OPERATION_REGISTRY,
    PermissionEngine,
    PermissionPolicy,
)
from keboola_agent_cli.services._sync_push_ops import SKIPPED_DELETIONS_REASON
from keboola_agent_cli.services._sync_stale import WORKSPACE_SYNC_OFF_REASON
from keboola_agent_cli.services._sync_workspace import (
    SANDBOXES_COMPONENT_ID,
    is_shared_sql_workspace,
    list_workspace_sessions,
)
from keboola_agent_cli.services.project_service import ProjectService
from keboola_agent_cli.services.sync_service import SyncService
from keboola_agent_cli.sync.manifest import load_manifest, save_manifest

BRANCH_ID = 12345
DEV_BRANCH_ID = 99999
TOKEN = "901-xxx"  # setup_single_project's token
PRIVATE_KEY_MARKER = "-----BEGIN PRIVATE KEY-----fake-key-material"

VERIFY_TOKEN = TokenVerifyResponse(
    token_id="tok-001",
    token_description="kbagent-cli",
    project_id=258,
    project_name="Production",
    owner_name="My Org",
)
BRANCHES = [
    {"id": BRANCH_ID, "name": "Main", "isDefault": True},
    {"id": DEV_BRANCH_ID, "name": "feature-x", "isDefault": False},
]

# A SQL workspace config as the UI creates it (keboola/ui sandboxes helpers.ts):
# runtime.shared, parameters.backendSize, read_only_storage_access only when
# off, SQL blocks, and the links a from-transformation workspace carries.
SHARED_SQL: dict[str, Any] = {
    "id": "ws-shared",
    "name": "Shared SQL",
    "description": "",
    "configuration": {
        "parameters": {
            "blocks": [
                {
                    "name": "Block 1",
                    "codes": [{"name": "Code 1", "script": ["SELECT 1;\n\nSELECT 2;"]}],
                }
            ],
            "backendSize": "small",
        },
        "storage": {
            "input": {
                "tables": [{"source": "in.c-main.orders", "destination": "orders"}],
                "read_only_storage_access": False,
            },
            "output": {"tables": []},
        },
        "runtime": {"shared": True},
        "shared_code_id": "sc-1",
        "shared_code_row_ids": ["sc-row-1"],
        "variables_id": "var-1",
        "variables_values_id": "var-values-1",
    },
    "rows": [],
}
PRIVATE_SQL: dict[str, Any] = {
    "id": "ws-private",
    "name": "Private SQL",
    "description": "",
    "configuration": {"parameters": {"blocks": []}, "runtime": {"shared": False}},
    "rows": [],
}
PYTHON_WS: dict[str, Any] = {
    "id": "ws-python",
    "name": "Python",
    "description": "",
    "configuration": {"parameters": {"id": "5551234"}, "runtime": {"shared": True}},
    "rows": [],
}
EXTRACTOR: dict[str, Any] = {
    "id": "keboola.ex-http",
    "type": "extractor",
    "configurations": [
        {
            "id": "cfg-001",
            "name": "My HTTP Extractor",
            "description": "",
            "configuration": {"parameters": {"baseUrl": "https://api.example.com"}},
            "rows": [],
        }
    ],
}
MCP_TOOL: dict[str, Any] = {
    "id": "keboola.mcp-server-tool",
    "type": "application",
    "configurations": [
        {"id": "mcp-001", "name": "MCP", "description": "", "configuration": {}, "rows": []}
    ],
}

SHARED_SQL_PATH = "other/keboola.sandboxes/shared-sql"


def _sandboxes(*configs: dict[str, Any]) -> dict[str, Any]:
    return {"id": SANDBOXES_COMPONENT_ID, "type": "other", "configurations": list(configs)}


def _remote(*configs: dict[str, Any]) -> list[dict[str, Any]]:
    return [copy.deepcopy(EXTRACTOR), _sandboxes(*copy.deepcopy(list(configs)))]


def _client(components: list[dict[str, Any]] | None = None) -> MagicMock:
    """A fake ``KeboolaClient``: ``spec`` makes a call to a method it lacks fail."""
    client = MagicMock(spec=KeboolaClient)
    client.__enter__.return_value = client
    client.__exit__.return_value = False
    client.verify_token.return_value = VERIFY_TOKEN
    client.list_dev_branches.return_value = BRANCHES
    client.list_components_with_configs.return_value = components or []
    client.list_config_folder_metadata.return_value = {}
    client.list_editor_sessions.return_value = []
    # The workspace read right before a delete (the parameters.id check).
    client.get_config_detail.return_value = copy.deepcopy(SHARED_SQL)
    return client


def _svc(store: Any, client: MagicMock) -> SyncService:
    return SyncService(config_store=store, client_factory=lambda url, token: client)


def _init(tmp_config_dir: Path, root: Path, *, sync_workspaces: bool) -> Any:
    store = setup_single_project(tmp_config_dir)
    _svc(store, _client()).init_sync(
        alias="prod", project_root=root, sync_workspaces=sync_workspaces
    )
    return store


def _pull(store: Any, root: Path, components: list[dict[str, Any]]) -> dict[str, Any]:
    return _svc(store, _client(components)).pull(
        alias="prod", project_root=root, no_storage=True, no_jobs=True
    )


def _tracked(root: Path) -> set[str]:
    return {
        cfg.id
        for cfg in load_manifest(root).configurations
        if cfg.component_id == SANDBOXES_COMPONENT_ID
    }


def _config_file(root: Path, rel_path: str = SHARED_SQL_PATH) -> Path:
    return root / "main" / rel_path / CONFIG_FILENAME


def _edit(root: Path, edit: Any, rel_path: str = SHARED_SQL_PATH) -> None:
    path = _config_file(root, rel_path)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    edit(data)
    path.write_text(yaml.dump(data, sort_keys=False), encoding="utf-8")


def _set_manifest_key(root: Path, key: str, value: Any) -> None:
    path = root / KEBOOLA_DIR_NAME / "manifest.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if value is None:
        data.pop(key, None)
    else:
        data[key] = value
    path.write_text(json.dumps(data, indent=4), encoding="utf-8")


def _session(
    session_id: str,
    config_id: str,
    *,
    branch_id: int = BRANCH_ID,
    component_id: str = SANDBOXES_COMPONENT_ID,
) -> dict[str, Any]:
    """A session shaped like the editor-service swagger ``SqlEditorSession``.

    ``snowflakePrivateKey`` is present although the service sends it only with
    ``includeCredentials``: the tests prove it never reaches kbagent's output.
    """
    return {
        "id": session_id,
        "status": "ready",
        "userId": "42",
        "branchId": str(branch_id),
        "componentId": component_id,
        "configurationId": config_id,
        "workspaceId": "9001",
        "workspaceSchema": "WORKSPACE_9001",
        "backendType": "snowflake",
        "shared": True,
        "snowflakePrivateKey": PRIVATE_KEY_MARKER,
    }


# ---------------------------------------------------------------------------
# Manifest key
# ---------------------------------------------------------------------------


class TestManifestKey:
    def test_key_written_only_when_on(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        off, on = tmp_path / "off", tmp_path / "on"
        off.mkdir()
        on.mkdir()
        _init(tmp_config_dir, off, sync_workspaces=False)
        store = setup_single_project(tmp_path / "cfg2")
        result = _svc(store, _client()).init_sync(
            alias="prod", project_root=on, sync_workspaces=True
        )

        assert result["sync_workspaces"] is True
        assert "syncWorkspaces" not in json.loads((off / ".keboola" / "manifest.json").read_text())
        assert json.loads((on / ".keboola" / "manifest.json").read_text())["syncWorkspaces"] is True

    def test_save_keeps_unknown_keys(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        """Keys kbagent does not model (a kbc ``templates`` block, a future key)
        survive a load/save round trip."""
        root = tmp_path / "project"
        root.mkdir()
        _init(tmp_config_dir, root, sync_workspaces=True)
        _set_manifest_key(root, "templates", {"repositories": [{"name": "keboola"}]})
        _set_manifest_key(root, "someFutureKey", 7)

        save_manifest(root, load_manifest(root))

        data = json.loads((root / ".keboola" / "manifest.json").read_text())
        assert data["templates"] == {"repositories": [{"name": "keboola"}]}
        assert data["someFutureKey"] == 7
        assert data["syncWorkspaces"] is True

    def test_adopt_existing_turns_key_on(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        store = _init(tmp_config_dir, root, sync_workspaces=False)

        result = _svc(store, _client()).init_sync(
            alias="prod", project_root=root, adopt_existing=True, sync_workspaces=True
        )

        assert result["status"] == "adopted"
        assert result["sync_workspaces"] is True
        assert load_manifest(root).sync_workspaces is True


# ---------------------------------------------------------------------------
# Which workspaces are in scope
# ---------------------------------------------------------------------------


class TestScopeRule:
    def test_shared_sql_workspace(self) -> None:
        assert is_shared_sql_workspace(SHARED_SQL)

    def test_empty_parameters_id_counts_as_sql(self) -> None:
        cfg = {"configuration": {"parameters": {"id": ""}, "runtime": {"shared": True}}}
        assert is_shared_sql_workspace(cfg)

    def test_not_shared_is_out(self) -> None:
        assert not is_shared_sql_workspace(PRIVATE_SQL)
        assert not is_shared_sql_workspace({"configuration": {"parameters": {}}})

    def test_python_and_legacy_sql_sandbox_are_out(self) -> None:
        """``parameters.id`` = a Data Science /apps record: Python/R, or a legacy
        SQL sandbox from before the SQL editor. Both stay skipped."""
        assert not is_shared_sql_workspace(PYTHON_WS)


# ---------------------------------------------------------------------------
# Pull / diff
# ---------------------------------------------------------------------------


class TestPullDiff:
    def test_opt_in_off_skips_all_workspaces(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        store = _init(tmp_config_dir, root, sync_workspaces=False)

        _pull(store, root, _remote(SHARED_SQL, PRIVATE_SQL, PYTHON_WS))

        assert _tracked(root) == set()
        assert not list(root.rglob("*keboola.sandboxes*"))
        diff = _svc(store, _client(_remote(SHARED_SQL))).diff(alias="prod", project_root=root)
        assert diff["summary"]["remote_only"] == 0

    def test_opt_in_on_pulls_shared_sql_only(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        store = _init(tmp_config_dir, root, sync_workspaces=True)

        _pull(store, root, _remote(SHARED_SQL, PRIVATE_SQL, PYTHON_WS))

        assert _tracked(root) == {"ws-shared"}
        local = yaml.safe_load(_config_file(root).read_text(encoding="utf-8"))
        assert local["parameters"]["backendSize"] == "small"
        assert local["parameters"]["blocks"][0]["codes"][0]["script"] == ["SELECT 1;\n\nSELECT 2;"]
        assert local["input"]["read_only_storage_access"] is False
        assert local["input"]["tables"][0]["source"] == "in.c-main.orders"
        assert local["_configuration_extra"] == {
            "runtime": {"shared": True},
            "shared_code_id": "sc-1",
            "shared_code_row_ids": ["sc-row-1"],
            "variables_id": "var-1",
            "variables_values_id": "var-values-1",
        }

    def test_no_phantom_diff_after_pull(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        store = _init(tmp_config_dir, root, sync_workspaces=True)
        remote = _remote(SHARED_SQL, PRIVATE_SQL, PYTHON_WS)
        _pull(store, root, remote)

        diff = _svc(store, _client(remote)).diff(alias="prod", project_root=root)

        assert diff["changes"] == []
        assert diff["remote_only"] == []
        again = _pull(store, root, remote)
        assert again["configs_pulled"] == 0

    def test_ignored_components_entry_wins(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        store = _init(tmp_config_dir, root, sync_workspaces=True)
        _set_manifest_key(root, "ignoredComponents", [SANDBOXES_COMPONENT_ID])

        _pull(store, root, _remote(SHARED_SQL))

        assert _tracked(root) == set()

    def test_mcp_server_tool_stays_ignored(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        store = _init(tmp_config_dir, root, sync_workspaces=True)

        _pull(store, root, [*_remote(SHARED_SQL), copy.deepcopy(MCP_TOOL)])

        components = {cfg.component_id for cfg in load_manifest(root).configurations}
        assert components == {"keboola.ex-http", SANDBOXES_COMPONENT_ID}

    def test_turning_key_off_drops_entries_as_ignored(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "project"
        root.mkdir()
        store = _init(tmp_config_dir, root, sync_workspaces=True)
        _pull(store, root, _remote(SHARED_SQL))
        assert _config_file(root).exists()

        _set_manifest_key(root, "syncWorkspaces", None)
        result = _pull(store, root, _remote(SHARED_SQL))

        actions = {
            d["action"] for d in result["details"] if d["component_id"] == SANDBOXES_COMPONENT_ID
        }
        assert actions == {"ignored"}
        assert _tracked(root) == set()
        assert not _config_file(root).exists()

    def test_turning_key_off_keeps_an_edited_workspace(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Un-pushed SQL in a workspace survives turning the key off: plain pull and
        ``--force`` keep the directory and the entry (``skipped``); ``--theirs``
        deletes it."""
        root = tmp_path / "project"
        root.mkdir()
        store = _init(tmp_config_dir, root, sync_workspaces=True)
        _pull(store, root, _remote(SHARED_SQL))
        _edit(root, lambda d: d["parameters"]["blocks"][0]["codes"][0].update(script=["SELECT 3;"]))
        _set_manifest_key(root, "syncWorkspaces", None)

        for force in (False, True):
            result = _svc(store, _client(_remote(SHARED_SQL))).pull(
                alias="prod", project_root=root, force=force, no_storage=True, no_jobs=True
            )
            [kept] = [d for d in result["details"] if d["component_id"] == SANDBOXES_COMPONENT_ID]
            assert kept["action"] == "skipped"
            assert kept["reason"] == WORKSPACE_SYNC_OFF_REASON
            assert _tracked(root) == {"ws-shared"}
            assert "SELECT 3;" in _config_file(root).read_text(encoding="utf-8")

        # Diff never plans a push for the kept entry: the component is ignored.
        diff = _svc(store, _client(_remote(SHARED_SQL))).diff(alias="prod", project_root=root)
        assert diff["changes"] == []

        result = _svc(store, _client(_remote(SHARED_SQL))).pull(
            alias="prod", project_root=root, theirs=True, no_storage=True, no_jobs=True
        )
        assert {d["action"] for d in result["details"]} == {"ignored"}
        assert _tracked(root) == set()
        assert not _config_file(root).exists()

    def test_tracked_workspace_made_private_stays_tracked(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Someone turns ``runtime.shared`` off in the UI. Diff reports the remote
        change; the entry does not vanish from the listing, which diff would read
        as a local ``added`` and push would create a second workspace for."""
        root = tmp_path / "project"
        root.mkdir()
        store = _init(tmp_config_dir, root, sync_workspaces=True)
        _pull(store, root, _remote(SHARED_SQL))
        made_private = copy.deepcopy(SHARED_SQL)
        made_private["configuration"]["runtime"]["shared"] = False
        client = _client(_remote(made_private))

        diff = _svc(store, client).diff(alias="prod", project_root=root)
        push = _svc(store, client).push(alias="prod", project_root=root)

        assert [c["change_type"] for c in diff["changes"]] == ["remote_modified"]
        assert push["status"] == "no_changes"
        client.create_config.assert_not_called()
        _pull(store, root, _remote(made_private))
        assert _tracked(root) == {"ws-shared"}


# ---------------------------------------------------------------------------
# Push: create / update
# ---------------------------------------------------------------------------


def _write_new_workspace(root: Path) -> None:
    config_dir = root / "main" / "other" / SANDBOXES_COMPONENT_ID / "new-ws"
    config_dir.mkdir(parents=True)
    (config_dir / CONFIG_FILENAME).write_text(
        yaml.dump(
            {
                "version": 2,
                "name": "New WS",
                "parameters": {"blocks": [], "backendSize": "small"},
                "input": {"tables": [{"source": "in.c-main.orders", "destination": "orders"}]},
                "_configuration_extra": {"runtime": {"shared": True}},
                "_keboola": {"component_id": SANDBOXES_COMPONENT_ID, "config_id": ""},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


# Every client method a config-only push or clone may call: reads, and the
# Storage configuration writes. Anything else (a Queue job, a SQL editor
# session, a workspace load) fails ``_assert_config_only``.
_CONFIG_ONLY_CALLS = frozenset(
    {
        "__enter__",
        "__exit__",
        "verify_token",
        "list_dev_branches",
        "list_components_with_configs",
        "list_config_folder_metadata",
        "get_config_detail",
        "create_config",
        "update_config",
        "set_config_metadata",
        "list_tables",
    }
)


def _assert_config_only(client: MagicMock) -> None:
    """No Queue job, no SQL editor session, no table load: only config calls."""
    called = {name.split(".")[0].split("(")[0] for name, _args, _kwargs in client.mock_calls}
    assert called <= _CONFIG_ONLY_CALLS, called - _CONFIG_ONLY_CALLS
    client.create_job.assert_not_called()
    client.load_workspace_tables.assert_not_called()
    client.list_editor_sessions.assert_not_called()


def _assert_no_credentials(result: dict[str, Any]) -> None:
    dumped = json.dumps(result, default=str)
    assert TOKEN not in dumped
    assert PRIVATE_KEY_MARKER not in dumped
    assert "snowflakePrivateKey" not in dumped


class TestPushCreateUpdate:
    def test_create_is_config_only(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        store = _init(tmp_config_dir, root, sync_workspaces=True)
        _pull(store, root, _remote())
        _write_new_workspace(root)
        client = _client(_remote())
        client.create_config.return_value = {"id": "ws-new", "name": "New WS"}

        result = _svc(store, client).push(alias="prod", project_root=root)

        assert result["created"] == 1, result
        kwargs = client.create_config.call_args.kwargs
        assert kwargs["component_id"] == SANDBOXES_COMPONENT_ID
        assert kwargs["configuration"]["runtime"] == {"shared": True}
        assert kwargs["configuration"]["parameters"]["backendSize"] == "small"
        assert kwargs["branch_id"] == BRANCH_ID
        _assert_config_only(client)
        _assert_no_credentials(result)
        assert _tracked(root) == {"ws-new"}

    def test_create_in_dev_branch(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        store = _init(tmp_config_dir, root, sync_workspaces=True)
        _pull(store, root, _remote())
        _write_new_workspace(root)
        client = _client(_remote())
        client.create_config.return_value = {"id": "ws-new"}

        _svc(store, client).push(alias="prod", project_root=root, branch_override=DEV_BRANCH_ID)

        assert client.create_config.call_args.kwargs["branch_id"] == DEV_BRANCH_ID
        _assert_config_only(client)

    def test_update_warns_on_backend_size_change(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "project"
        root.mkdir()
        store = _init(tmp_config_dir, root, sync_workspaces=True)
        _pull(store, root, _remote(SHARED_SQL))
        _edit(root, lambda d: d["parameters"].update(backendSize="large"))
        client = _client(_remote(SHARED_SQL))
        client.get_config_detail.return_value = copy.deepcopy(SHARED_SQL)
        client.update_config.return_value = copy.deepcopy(SHARED_SQL)

        result = _svc(store, client).push(alias="prod", project_root=root)

        assert result["updated"] == 1, result
        sent = client.update_config.call_args.kwargs["configuration"]
        assert sent["parameters"]["backendSize"] == "large"
        size_warnings = [
            w for w in result.get("warnings", []) if w["change_type"] == "workspace_backend_size"
        ]
        assert len(size_warnings) == 1
        assert size_warnings[0]["old_backend_size"] == "small"
        assert size_warnings[0]["new_backend_size"] == "large"
        assert "session created after this push" in size_warnings[0]["message"]
        _assert_config_only(client)
        _assert_no_credentials(result)

    def test_update_without_size_change_has_no_warning(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "project"
        root.mkdir()
        store = _init(tmp_config_dir, root, sync_workspaces=True)
        _pull(store, root, _remote(SHARED_SQL))
        _edit(root, lambda d: d["input"]["tables"].append({"source": "in.c-main.x"}))
        client = _client(_remote(SHARED_SQL))
        client.get_config_detail.return_value = copy.deepcopy(SHARED_SQL)
        client.update_config.return_value = copy.deepcopy(SHARED_SQL)

        result = _svc(store, client).push(alias="prod", project_root=root)

        assert result["updated"] == 1
        assert "warnings" not in result


# ---------------------------------------------------------------------------
# Push: delete (sessions first, then the config)
# ---------------------------------------------------------------------------


def _pulled_then_deleted_locally(tmp_config_dir: Path, root: Path) -> Any:
    store = _init(tmp_config_dir, root, sync_workspaces=True)
    _pull(store, root, _remote(SHARED_SQL))
    shutil.rmtree(_config_file(root).parent)
    return store


def _sessions() -> list[dict[str, Any]]:
    return [
        _session("s-mine", "ws-shared"),
        _session("s-other-user", "ws-shared"),
        _session("s-dev-branch", "ws-shared", branch_id=DEV_BRANCH_ID),
        _session("s-other-config", "ws-other"),
        _session("s-transformation", "ws-shared", component_id="keboola.snowflake-transformation"),
    ]


class TestPushDelete:
    def test_plain_push_holds_back_the_workspace_delete(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """Without --force push touches neither the sessions nor the config (#792 G)."""
        root = tmp_path / "project"
        root.mkdir()
        store = _pulled_then_deleted_locally(tmp_config_dir, root)
        client = _client(_remote(SHARED_SQL))
        client.list_editor_sessions.return_value = _sessions()

        result = _svc(store, client).push(alias="prod", project_root=root)

        assert result["deleted"] == 0
        assert [c["config_id"] for c in result["skipped_deletions"]] == ["ws-shared"]
        client.get_config_detail.assert_not_called()
        client.list_editor_sessions.assert_not_called()
        client.delete_editor_session.assert_not_called()
        client.delete_config.assert_not_called()
        assert _tracked(root) == {"ws-shared"}

    def test_held_back_workspace_delete_explains_force(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """The reason for a held-back workspace delete says what --force would also do."""
        root = tmp_path / "project"
        root.mkdir()
        store = _pulled_then_deleted_locally(tmp_config_dir, root)

        result = _svc(store, _client(_remote(SHARED_SQL))).push(alias="prod", project_root=root)

        reason = result["skipped_deletions_reason"]
        assert reason.startswith(SKIPPED_DELETIONS_REASON)
        assert "SQL editor sessions of all users" in reason
        assert "cannot be restored" in reason
        assert "sync push --dry-run --force" in reason

    def test_held_back_plain_delete_has_no_workspace_note(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "project"
        root.mkdir()
        store = _init(tmp_config_dir, root, sync_workspaces=True)
        _pull(store, root, _remote(SHARED_SQL))
        extractor_path = next(
            cfg.path for cfg in load_manifest(root).configurations if cfg.id == "cfg-001"
        )
        shutil.rmtree(root / "main" / extractor_path)

        result = _svc(store, _client(_remote(SHARED_SQL))).push(alias="prod", project_root=root)

        assert [c["config_id"] for c in result["skipped_deletions"]] == ["cfg-001"]
        assert result["skipped_deletions_reason"] == SKIPPED_DELETIONS_REASON

    def test_dry_run_flags_an_app_backed_workspace(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """The preview applies the parameters.id check of the real delete."""
        root = tmp_path / "project"
        root.mkdir()
        store = _pulled_then_deleted_locally(tmp_config_dir, root)
        client = _client(_remote(SHARED_SQL))
        app_backed = copy.deepcopy(SHARED_SQL)
        app_backed["configuration"]["parameters"]["id"] = "5551234"
        client.get_config_detail.return_value = app_backed
        client.list_editor_sessions.return_value = _sessions()

        result = _svc(store, client).push(alias="prod", project_root=root, dry_run=True, force=True)

        [warning] = result["warnings"]
        assert warning["change_type"] == "workspace_delete_refused"
        assert "Push will refuse this delete" in warning["message"]
        assert "5551234" in warning["message"]
        client.list_editor_sessions.assert_not_called()
        client.delete_config.assert_not_called()

    def test_plain_dry_run_previews_no_session_delete(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """The preview reads what plan_push would apply: no --force, no delete to preview."""
        root = tmp_path / "project"
        root.mkdir()
        store = _pulled_then_deleted_locally(tmp_config_dir, root)
        client = _client(_remote(SHARED_SQL))
        client.list_editor_sessions.return_value = _sessions()

        result = _svc(store, client).push(alias="prod", project_root=root, dry_run=True)

        assert [c["config_id"] for c in result["skipped_deletions"]] == ["ws-shared"]
        assert not [
            w for w in result.get("warnings", []) if w["change_type"].startswith("workspace")
        ]
        client.list_editor_sessions.assert_not_called()

    def test_force_delete_removes_sessions_then_config(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "project"
        root.mkdir()
        store = _pulled_then_deleted_locally(tmp_config_dir, root)
        client = _client(_remote(SHARED_SQL))
        client.list_editor_sessions.return_value = _sessions()
        order = MagicMock()
        order.attach_mock(client.delete_editor_session, "delete_editor_session")
        order.attach_mock(client.delete_config, "delete_config")

        result = _svc(store, client).push(alias="prod", project_root=root, force=True)

        assert result["deleted"] == 1, result
        client.list_editor_sessions.assert_called_once_with(branch_id=BRANCH_ID)
        assert order.mock_calls == [
            call.delete_editor_session("s-mine"),
            call.delete_editor_session("s-other-user"),
            call.delete_config(
                component_id=SANDBOXES_COMPONENT_ID, config_id="ws-shared", branch_id=BRANCH_ID
            ),
        ]
        assert result["pushed_details"][0]["deleted_session_ids"] == ["s-mine", "s-other-user"]
        assert _tracked(root) == set()
        _assert_no_credentials(result)

    def test_session_list_failure_keeps_config(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        store = _pulled_then_deleted_locally(tmp_config_dir, root)
        client = _client(_remote(SHARED_SQL))
        client.list_editor_sessions.side_effect = KeboolaApiError(
            message="Editor service unavailable",
            status_code=503,
            error_code=ErrorCode.API_ERROR,
        )

        result = _svc(store, client).push(alias="prod", project_root=root, force=True)

        client.delete_config.assert_not_called()
        client.delete_editor_session.assert_not_called()
        assert result["deleted"] == 0
        assert result["errors"][0]["config_id"] == "ws-shared"
        assert "Editor service unavailable" in result["errors"][0]["message"]
        assert _tracked(root) == {"ws-shared"}

    def test_session_delete_failure_keeps_config(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "project"
        root.mkdir()
        store = _pulled_then_deleted_locally(tmp_config_dir, root)
        client = _client(_remote(SHARED_SQL))
        client.list_editor_sessions.return_value = _sessions()
        client.delete_editor_session.side_effect = KeboolaApiError(
            message="Cannot delete session during initialization.",
            status_code=400,
            error_code=ErrorCode.API_ERROR,
        )

        result = _svc(store, client).push(alias="prod", project_root=root, force=True)

        client.delete_config.assert_not_called()
        assert result["errors"][0]["config_id"] == "ws-shared"
        assert _tracked(root) == {"ws-shared"}

    def test_partial_session_delete_failure_reports_deleted_ids(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "project"
        root.mkdir()
        store = _pulled_then_deleted_locally(tmp_config_dir, root)
        client = _client(_remote(SHARED_SQL))
        client.list_editor_sessions.return_value = _sessions()
        client.delete_editor_session.side_effect = [
            None,
            KeboolaApiError(
                message="Server error", status_code=500, error_code=ErrorCode.API_ERROR
            ),
        ]

        result = _svc(store, client).push(alias="prod", project_root=root, force=True)

        client.delete_config.assert_not_called()
        [error] = result["errors"]
        assert error["config_id"] == "ws-shared"
        assert "s-other-user could not be deleted" in error["message"]
        assert "Sessions already deleted: s-mine." in error["message"]
        assert _tracked(root) == {"ws-shared"}

    def test_session_already_gone_counts_as_deleted(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A retried DELETE after a lost 204 answers 404: the session is gone."""
        root = tmp_path / "project"
        root.mkdir()
        store = _pulled_then_deleted_locally(tmp_config_dir, root)
        client = _client(_remote(SHARED_SQL))
        client.list_editor_sessions.return_value = _sessions()
        client.delete_editor_session.side_effect = [
            KeboolaApiError(message="Not found", status_code=404, error_code=ErrorCode.NOT_FOUND),
            None,
        ]

        result = _svc(store, client).push(alias="prod", project_root=root, force=True)

        assert result["errors"] == []
        assert result["deleted"] == 1
        assert result["pushed_details"][0]["deleted_session_ids"] == ["s-mine", "s-other-user"]
        client.delete_config.assert_called_once()

    def test_failed_config_delete_names_the_deleted_sessions(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """The sessions are gone when the config delete fails; the error says so."""
        root = tmp_path / "project"
        root.mkdir()
        store = _pulled_then_deleted_locally(tmp_config_dir, root)
        client = _client(_remote(SHARED_SQL))
        client.list_editor_sessions.return_value = _sessions()
        client.delete_config.side_effect = KeboolaApiError(
            message="Storage timed out", status_code=504, error_code=ErrorCode.TIMEOUT
        )

        result = _svc(store, client).push(alias="prod", project_root=root, force=True)

        assert client.delete_editor_session.call_count == 2
        [error] = result["errors"]
        assert error["config_id"] == "ws-shared"
        assert "Storage timed out" in error["message"]
        assert "already deleted: s-mine, s-other-user." in error["message"]
        assert _tracked(root) == {"ws-shared"}

    def test_no_default_branch_deletes_nothing(
        self, tmp_config_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Production resolves to branch None (e.g. git-branching on the default git
        branch). Without a default branch id the sessions cannot be filtered by
        branch, so push must refuse instead of deleting the config and leaving
        the sessions running."""
        root = tmp_path / "project"
        root.mkdir()
        store = _pulled_then_deleted_locally(tmp_config_dir, root)
        client = _client(_remote(SHARED_SQL))
        client.list_editor_sessions.return_value = _sessions()
        client.list_dev_branches.return_value = [{"id": DEV_BRANCH_ID, "isDefault": False}]
        svc = _svc(store, client)
        monkeypatch.setattr(svc, "_resolve_branch_id", lambda *args, **kwargs: None)

        result = svc.push(alias="prod", project_root=root, force=True)

        [error] = result["errors"]
        assert error["config_id"] == "ws-shared"
        assert "default branch" in error["message"]
        client.list_editor_sessions.assert_not_called()
        client.delete_editor_session.assert_not_called()
        client.delete_config.assert_not_called()
        assert _tracked(root) == {"ws-shared"}

    def test_app_backed_workspace_is_not_deleted(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        """A tracked workspace whose config now has parameters.id is backed by a Data
        Science app; a config-only delete would leave the app running."""
        root = tmp_path / "project"
        root.mkdir()
        store = _pulled_then_deleted_locally(tmp_config_dir, root)
        client = _client(_remote(SHARED_SQL))
        app_backed = copy.deepcopy(SHARED_SQL)
        app_backed["configuration"]["parameters"]["id"] = "5551234"
        client.get_config_detail.return_value = app_backed

        result = _svc(store, client).push(alias="prod", project_root=root, force=True)

        [error] = result["errors"]
        assert error["error_code"] == ErrorCode.VALIDATION_ERROR
        assert "parameters.id" in error["message"]
        client.list_editor_sessions.assert_not_called()
        client.delete_editor_session.assert_not_called()
        client.delete_config.assert_not_called()
        assert _tracked(root) == {"ws-shared"}

    def test_dry_run_lists_sessions_and_deletes_nothing(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "project"
        root.mkdir()
        store = _pulled_then_deleted_locally(tmp_config_dir, root)
        client = _client(_remote(SHARED_SQL))
        client.list_editor_sessions.return_value = _sessions()

        result = _svc(store, client).push(alias="prod", project_root=root, dry_run=True, force=True)

        assert result["status"] == "dry_run"
        [preview] = [w for w in result["warnings"] if w["change_type"] == "workspace_sessions"]
        assert preview["config_id"] == "ws-shared"
        assert preview["session_count"] == 2
        assert preview["session_ids"] == ["s-mine", "s-other-user"]
        client.delete_editor_session.assert_not_called()
        client.delete_config.assert_not_called()
        _assert_no_credentials(result)

    def test_dry_run_reports_listing_failure(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        root = tmp_path / "project"
        root.mkdir()
        store = _pulled_then_deleted_locally(tmp_config_dir, root)
        client = _client(_remote(SHARED_SQL))
        client.list_editor_sessions.side_effect = KeboolaApiError(
            message="Editor service unavailable", status_code=503
        )

        result = _svc(store, client).push(alias="prod", project_root=root, dry_run=True, force=True)

        [warning] = result["warnings"]
        assert warning["change_type"] == "workspace_sessions_unknown"
        assert "would not delete" in warning["message"]

    def test_dry_run_without_workspace_delete_makes_no_editor_call(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        root = tmp_path / "project"
        root.mkdir()
        store = _init(tmp_config_dir, root, sync_workspaces=True)
        _pull(store, root, _remote(SHARED_SQL))
        _edit(root, lambda d: d["parameters"].update(backendSize="large"))
        client = _client(_remote(SHARED_SQL))

        result = _svc(store, client).push(alias="prod", project_root=root, dry_run=True)

        assert result["status"] == "dry_run"
        assert "warnings" not in result
        client.list_editor_sessions.assert_not_called()


class TestSessionListing:
    def test_production_without_branch_id_resolves_default_branch(self) -> None:
        client = _client()
        client.list_editor_sessions.return_value = _sessions()

        sessions = list_workspace_sessions(client, {"ws-shared"}, None)

        client.list_editor_sessions.assert_called_once_with(branch_id=BRANCH_ID)
        assert sessions == {"ws-shared": ["s-mine", "s-other-user"]}

    def test_dev_branch_sessions_only(self) -> None:
        client = _client()
        client.list_editor_sessions.return_value = _sessions()

        sessions = list_workspace_sessions(client, {"ws-shared"}, DEV_BRANCH_ID)

        assert sessions == {"ws-shared": ["s-dev-branch"]}


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------


class TestClone:
    def test_clone_creates_workspace_and_warns_on_missing_tables(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        source = tmp_path / "golden"
        source.mkdir()
        store = _init(tmp_config_dir, source, sync_workspaces=True)
        _pull(store, source, _remote(SHARED_SQL))
        client = _client([])  # fresh target
        new_ids = {"keboola.ex-http": "ext-new", SANDBOXES_COMPONENT_ID: "ws-new"}
        client.create_config.side_effect = lambda **kw: {"id": new_ids[kw["component_id"]]}
        client.list_tables.return_value = [{"id": "in.c-prod.customers"}]

        result = _svc(store, client).clone_project(
            source=source,
            target_alias="prod",
            target_dir=tmp_path / "clone",
            overrides={"bucket_map": {"in.c-main": "in.c-prod"}, "create_buckets": False},
        )

        assert result["status"] == "cloned", result
        created = {c.kwargs["component_id"]: c.kwargs for c in client.create_config.call_args_list}
        workspace = created[SANDBOXES_COMPONENT_ID]["configuration"]
        # bucket_map rewrote the workspace input mapping like any other config.
        assert workspace["storage"]["input"]["tables"][0]["source"] == "in.c-prod.orders"
        [warning] = result["warnings"]
        assert warning["change_type"] == "workspace_input_tables_missing"
        assert warning["missing_tables"] == ["in.c-prod.orders"]
        assert warning["config_id"] == "ws-new"
        _assert_config_only(client)

    def test_clone_without_workspaces_makes_no_table_call(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        source = tmp_path / "golden"
        source.mkdir()
        store = _init(tmp_config_dir, source, sync_workspaces=False)
        _pull(store, source, _remote(SHARED_SQL))
        client = _client([])
        client.create_config.return_value = {"id": "ext-new"}

        result = _svc(store, client).clone_project(
            source=source,
            target_alias="prod",
            target_dir=tmp_path / "clone",
            overrides={"create_buckets": False},
        )

        assert result["warnings"] == []
        client.list_tables.assert_not_called()


# ---------------------------------------------------------------------------
# CLI + Editor client
# ---------------------------------------------------------------------------


class TestSyncInitCli:
    def test_with_workspaces_is_forwarded(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        store = setup_single_project(tmp_config_dir)
        svc = MagicMock()
        svc.init_sync.return_value = {
            "status": "initialized",
            "project_id": 258,
            "project_alias": "prod",
            "api_host": "connection.keboola.com",
            "git_branching": False,
            "default_branch": "main",
            "sync_workspaces": True,
            "files_created": [],
        }
        with (
            patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
            patch(
                "keboola_agent_cli.cli.ProjectService",
                return_value=ProjectService(config_store=store),
            ),
            patch("keboola_agent_cli.cli.SyncService", return_value=svc),
        ):
            result = CliRunner().invoke(
                app,
                [
                    "--json",
                    "sync",
                    "init",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                    "--with-workspaces",
                ],
            )

        assert result.exit_code == 0, result.output
        assert svc.init_sync.call_args.kwargs["sync_workspaces"] is True
        assert json.loads(result.output)["data"]["sync_workspaces"] is True


STACK_URL = "https://connection.eu-central-1.keboola.com"
EDITOR_URL = "https://editor.eu-central-1.keboola.com"
CLIENT_TOKEN = "901-55555-fakeTestTokenDoNotUseXXXXXXXX"


class TestEditorClient:
    def test_list_sessions_of_all_users_in_branch(self, httpx_mock: Any) -> None:
        httpx_mock.add_response(
            url=f"{EDITOR_URL}/sql/sessions?listAll=1&branchId={BRANCH_ID}",
            method="GET",
            json=[_session("s-1", "ws-shared")],
        )

        with KeboolaClient(stack_url=STACK_URL, token=CLIENT_TOKEN) as client:
            sessions = client.list_editor_sessions(branch_id=BRANCH_ID)

        assert [s["id"] for s in sessions] == ["s-1"]
        request = httpx_mock.get_requests()[0]
        assert request.headers["X-StorageApi-Token"] == CLIENT_TOKEN
        assert "includeCredentials" not in str(request.url)

    def test_delete_session(self, httpx_mock: Any) -> None:
        httpx_mock.add_response(
            url=f"{EDITOR_URL}/sql/sessions/s%2F1", method="DELETE", status_code=204
        )

        with KeboolaClient(stack_url=STACK_URL, token=CLIENT_TOKEN) as client:
            client.delete_editor_session("s/1")

        assert str(httpx_mock.get_requests()[0].url).endswith("/sql/sessions/s%2F1")

    def test_non_list_body_raises(self, httpx_mock: Any) -> None:
        """An unexpected 200 body must not read as "no sessions" (fail closed)."""
        httpx_mock.add_response(
            url=f"{EDITOR_URL}/sql/sessions?listAll=1",
            method="GET",
            json={"sessions": [_session("s-1", "ws-shared")]},
        )

        with (
            KeboolaClient(stack_url=STACK_URL, token=CLIENT_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.list_editor_sessions()

        assert exc_info.value.error_code == ErrorCode.API_ERROR

    def test_invalid_json_raises_api_error(self, httpx_mock: Any) -> None:
        httpx_mock.add_response(
            url=f"{EDITOR_URL}/sql/sessions?listAll=1", method="GET", text="<html>proxy</html>"
        )

        with (
            KeboolaClient(stack_url=STACK_URL, token=CLIENT_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.list_editor_sessions()

        assert exc_info.value.error_code == ErrorCode.API_ERROR

    def test_list_with_a_non_object_item_raises(self, httpx_mock: Any) -> None:
        httpx_mock.add_response(
            url=f"{EDITOR_URL}/sql/sessions?listAll=1", method="GET", json=["s-1"]
        )

        with (
            KeboolaClient(stack_url=STACK_URL, token=CLIENT_TOKEN) as client,
            pytest.raises(KeboolaApiError),
        ):
            client.list_editor_sessions()

    def test_list_forbidden_raises(self, httpx_mock: Any) -> None:
        httpx_mock.add_response(
            url=f"{EDITOR_URL}/sql/sessions?listAll=1",
            method="GET",
            status_code=403,
            json={"error": "You do not have permission to modify sessions.", "code": 403},
        )

        with (
            KeboolaClient(stack_url=STACK_URL, token=CLIENT_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.list_editor_sessions()

        assert exc_info.value.status_code == 403

    def test_delete_404_raises_with_status(self, httpx_mock: Any) -> None:
        """The service layer reads a 404 on delete as "already gone"; the client
        reports the status so it can."""
        httpx_mock.add_response(
            url=f"{EDITOR_URL}/sql/sessions/s-1",
            method="DELETE",
            status_code=404,
            json={"error": "Session not found", "code": 404},
        )

        with (
            KeboolaClient(stack_url=STACK_URL, token=CLIENT_TOKEN) as client,
            pytest.raises(KeboolaApiError) as exc_info,
        ):
            client.delete_editor_session("s-1")

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Human output: warnings are plain text, never Rich markup
# ---------------------------------------------------------------------------

BRACKETED = [
    {"change_type": "workspace_sessions", "message": "Workspace Sales [prod] mart: 1 session"},
    {"change_type": "workspace_sessions", "message": "Workspace Tmp [/wip] copy: 2 sessions"},
]


def _invoke_with_sync_service(tmp_config_dir: Path, svc: MagicMock, args: list[str]) -> Any:
    store = setup_single_project(tmp_config_dir)
    with (
        patch("keboola_agent_cli.cli.ConfigStore", return_value=store),
        patch(
            "keboola_agent_cli.cli.ProjectService",
            return_value=ProjectService(config_store=store),
        ),
        patch("keboola_agent_cli.cli.SyncService", return_value=svc),
    ):
        return CliRunner().invoke(app, args)


def _stderr_text(result: Any) -> str:
    return " ".join(result.stderr.split())


class TestHumanWarnings:
    def test_push_dry_run_prints_bracketed_names(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        svc = MagicMock()
        svc.push.return_value = {
            "status": "dry_run",
            "changes": [],
            "summary": {"added": 0, "modified": 0, "deleted": 1},
            "warnings": BRACKETED,
        }

        result = _invoke_with_sync_service(
            tmp_config_dir,
            svc,
            ["sync", "push", "--project", "prod", "--directory", str(tmp_path), "--dry-run"],
        )

        assert result.exit_code == 0, result.output
        stderr = _stderr_text(result)
        assert "Workspace Sales [prod] mart: 1 session" in stderr
        assert "Workspace Tmp [/wip] copy: 2 sessions" in stderr

    def test_push_all_projects_verbose_prints_warnings(
        self, tmp_config_dir: Path, tmp_path: Path
    ) -> None:
        svc = MagicMock()
        svc.push_all.return_value = {
            "summary": {"total": 1, "success": 1, "failed": 0},
            "projects": {
                "prod": {
                    "status": "pushed",
                    "created": 0,
                    "updated": 1,
                    "deleted": 0,
                    "errors": [],
                    "warnings": [
                        {
                            "change_type": "workspace_backend_size",
                            "message": "Workspace [ws-1]: backendSize changed",
                        }
                    ],
                }
            },
            "skipped": [],
        }

        result = _invoke_with_sync_service(
            tmp_config_dir,
            svc,
            ["--verbose", "sync", "push", "--all-projects", "--directory", str(tmp_path)],
        )

        assert result.exit_code == 0, result.output
        assert "Workspace [ws-1]: backendSize changed" in _stderr_text(result)

    def test_clone_prints_bracketed_names(self, tmp_config_dir: Path, tmp_path: Path) -> None:
        source = tmp_path / "golden"
        (source / ".keboola").mkdir(parents=True)
        svc = MagicMock()
        svc.clone_project.return_value = {
            "status": "cloned",
            "target_alias": "prod",
            "target_dir": str(tmp_path / "clone"),
            "created": 1,
            "errors": [],
            "warnings": BRACKETED,
        }

        result = _invoke_with_sync_service(
            tmp_config_dir,
            svc,
            [
                "sync",
                "clone",
                "--source",
                str(source),
                "--target",
                "prod",
                "--target-dir",
                str(tmp_path / "clone"),
            ],
        )

        assert result.exit_code == 0, result.output
        stderr = _stderr_text(result)
        assert "Workspace Sales [prod] mart: 1 session" in stderr
        assert "Workspace Tmp [/wip] copy: 2 sessions" in stderr


# ---------------------------------------------------------------------------
# Permissions: `sync push --force` is destructive
# ---------------------------------------------------------------------------


class TestPushForcePermission:
    def test_force_is_a_destructive_escalation(self) -> None:
        assert FLAG_ESCALATIONS["sync.push --force"] == "destructive"
        assert "sync.push --force" not in OPERATION_REGISTRY
        engine = PermissionEngine(PermissionPolicy(mode="allow", deny=["cli:destructive"]))
        assert engine.is_allowed("sync.push") is True
        assert engine.is_allowed("sync.push --force") is False

    @pytest.mark.parametrize(
        ("mode", "allow", "deny", "force_allowed"),
        [
            # An allow-list that names only `sync.push` allows a plain push, not `--force`.
            ("deny", ["sync.push"], [], False),
            ("deny", ["sync.push", "sync.push --force"], [], True),
            ("deny", ["sync.*"], [], True),
            ("deny", ["sync.push", "cli:destructive"], [], True),
            # `cli:write` covers the destructive class, and `sync.push` does not match `--force`.
            ("allow", ["sync.push"], ["cli:write"], False),
            ("allow", ["sync.push", "sync.push --force"], ["cli:write"], True),
        ],
    )
    def test_allow_list_must_name_the_force_escalation(
        self, mode: str, allow: list[str], deny: list[str], force_allowed: bool
    ) -> None:
        engine = PermissionEngine(PermissionPolicy(mode=mode, allow=allow, deny=deny))
        assert engine.is_allowed("sync.push") is True
        assert engine.is_allowed("sync.push --force") is force_allowed

    def _config_dir(
        self,
        tmp_path: Path,
        deny: list[str],
        *,
        mode: str = "allow",
        allow: list[str] | None = None,
    ) -> Path:
        config_dir = tmp_path / "c"
        config_dir.mkdir()
        # Written directly: `permissions set` needs a human at a real terminal.
        (config_dir / "config.json").write_text(
            json.dumps(
                {
                    "version": CURRENT_CONFIG_VERSION,
                    "projects": {},
                    "permissions": {"mode": mode, "allow": allow or [], "deny": deny},
                }
            )
        )
        return config_dir

    def test_allow_list_with_only_sync_push_blocks_force(self, tmp_path: Path) -> None:
        config_dir = self._config_dir(tmp_path, [], mode="deny", allow=["sync.push"])

        denied, svc = self._push_with(config_dir, tmp_path, "--force")
        assert denied.exit_code == EXIT_PERMISSION_DENIED
        svc.push.assert_not_called()

        allowed, svc = self._push_with(config_dir, tmp_path)
        assert allowed.exit_code == 0, allowed.output
        svc.push.assert_called_once()

    def test_policy_denying_destructive_blocks_force_only(self, tmp_path: Path) -> None:
        config_dir = self._config_dir(tmp_path, ["cli:destructive"])

        denied, svc = self._push_with(config_dir, tmp_path, "--force")
        assert denied.exit_code == EXIT_PERMISSION_DENIED
        svc.push.assert_not_called()

        allowed, svc = self._push_with(config_dir, tmp_path)
        assert allowed.exit_code == 0, allowed.output
        svc.push.assert_called_once()

    def test_deny_destructive_flag_blocks_force(self, tmp_path: Path) -> None:
        config_dir = self._config_dir(tmp_path, [])
        svc = MagicMock()
        with patch("keboola_agent_cli.cli.SyncService", return_value=svc):
            result = CliRunner().invoke(
                app,
                [
                    "--config-dir",
                    str(config_dir),
                    "--deny-destructive",
                    "--json",
                    "sync",
                    "push",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                    "--force",
                ],
            )
        assert result.exit_code == EXIT_PERMISSION_DENIED
        svc.push.assert_not_called()

    def _push_with(self, config_dir: Path, tmp_path: Path, *flags: str) -> tuple[Any, MagicMock]:
        svc = MagicMock()
        svc.push.return_value = {"status": "no_changes", "created": 0, "updated": 0, "deleted": 0}
        with patch("keboola_agent_cli.cli.SyncService", return_value=svc):
            result = CliRunner().invoke(
                app,
                [
                    "--config-dir",
                    str(config_dir),
                    "--json",
                    "sync",
                    "push",
                    "--project",
                    "prod",
                    "--directory",
                    str(tmp_path),
                    *flags,
                ],
            )
        return result, svc
